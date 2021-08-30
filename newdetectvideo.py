import time
import tensorflow as tf

physical_devices = tf.config.experimental.list_physical_devices('GPU')
if len(physical_devices) > 0:
    tf.config.experimental.set_memory_growth(physical_devices[0], True)
from absl import app, flags, logging
from absl.flags import FLAGS
import core.utils as utils
from core.yolov4 import filter_boxes, decode, YOLO
from tensorflow.python.saved_model import tag_constants
from PIL import Image
import cv2
import numpy as np

# from tensorflow.compat.v1 import ConfigProto
# from tensorflow.compat.v1 import InteractiveSession

flags.DEFINE_string('framework', 'tf', '(tf, tflite, trt')
flags.DEFINE_string('weights', './checkpoints/yolov4-416/variables/variables',
                    'path to weights file')
flags.DEFINE_integer('size', 416, 'resize images to')
flags.DEFINE_boolean('tiny', False, 'yolo or yolo-tiny')
flags.DEFINE_string('model', 'yolov4', 'yolov3 or yolov4')
# flags.DEFINE_string('video', './data/road.mp4', 'path to input video')
flags.DEFINE_string('video', None, 'path to input video')
flags.DEFINE_float('iou', 0.45, 'iou threshold')
flags.DEFINE_float('score', 0.25, 'score threshold')
flags.DEFINE_string('output', None, 'path to output video')
flags.DEFINE_string('output_format', 'XVID', 'codec used in VideoWriter when saving video to file')
flags.DEFINE_boolean('dis_cv2_window', True, 'disable cv2 window during the process') # this is good for the .ipynb


# @tf.function
def infer(batch_data, model, STRIDES, ANCHORS, NUM_CLASS, XYSCALE):
    # batch_data = tf.constant(image_data)
    feature_maps = model(batch_data)
    bbox_tensors = []
    prob_tensors = []
    if FLAGS.tiny:
        for i, fm in enumerate(feature_maps):
            if i == 0:
                output_tensors = decode(fm, FLAGS.size // 16, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE,
                                        FLAGS.framework)
            else:
                output_tensors = decode(fm, FLAGS.size // 32, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE,
                                        FLAGS.framework)
            bbox_tensors.append(output_tensors[0])
            prob_tensors.append(output_tensors[1])
    else:
        for i, fm in enumerate(feature_maps):
            if i == 0:
                output_tensors = decode(fm, FLAGS.size // 8, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE,
                                        FLAGS.framework)
            elif i == 1:
                output_tensors = decode(fm, FLAGS.size // 16, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE,
                                        FLAGS.framework)
            else:
                output_tensors = decode(fm, FLAGS.size // 32, NUM_CLASS, STRIDES, ANCHORS, i, XYSCALE,
                                        FLAGS.framework)
            bbox_tensors.append(output_tensors[0])
            prob_tensors.append(output_tensors[1])

    pred_bbox = tf.concat(bbox_tensors, axis=1)
    pred_prob = tf.concat(prob_tensors, axis=1)
    if FLAGS.framework == 'tflite':
        pred_bbox = (pred_bbox, pred_prob)
    else:
        boxes, pred_conf = filter_boxes(pred_bbox, pred_prob, score_threshold=FLAGS.score,
                                        input_shape=tf.constant([FLAGS.size, FLAGS.size]))
        pred_bbox = tf.concat([boxes, pred_conf], axis=-1)
    boxes = pred_bbox[:, :, 0:4]
    pred_conf = pred_bbox[:, :, 4:]

    boxes, scores, classes, valid_detections = tf.image.combined_non_max_suppression(
        boxes=tf.reshape(boxes, (tf.shape(boxes)[0], -1, 1, 4)),
        scores=tf.reshape(
            pred_conf, (tf.shape(pred_conf)[0], -1, tf.shape(pred_conf)[-1])),
        max_output_size_per_class=50,
        max_total_size=50,
        iou_threshold=FLAGS.iou,
        score_threshold=FLAGS.score
    )
    return boxes, scores, classes, valid_detections


def main(_argv):
    # config = ConfigProto()
    # config.gpu_options.allow_growth = True
    # session = InteractiveSession(config=config)
    input_size = FLAGS.size
    video_path = FLAGS.video

    STRIDES, ANCHORS, NUM_CLASS, XYSCALE = utils.load_config(FLAGS)

    print("Video from: ", video_path)
    vid = cv2.VideoCapture(video_path)

    if FLAGS.framework == 'tflite':
        interpreter = tf.lite.Interpreter(model_path=FLAGS.weights)
        interpreter.allocate_tensors()
        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()
        print(input_details)
        print(output_details)
    else:
        # saved_model_loaded = tf.saved_model.load(FLAGS.weights, tags=[tag_constants.SERVING])
        # infer = saved_model_loaded.signatures[tf.saved_model.DEFAULT_SERVING_SIGNATURE_DEF_KEY]
        # STRIDES, ANCHORS, NUM_CLASS, XYSCALE = utils.load_config(FLAGS)
        inputs = tf.keras.layers.Input([FLAGS.size, FLAGS.size, 3])
        outputs = YOLO(inputs, NUM_CLASS, FLAGS.model, FLAGS.tiny)
        model = tf.keras.Model(inputs, outputs)
        model.load_weights(FLAGS.weights)

    if FLAGS.output:
        # by default VideoCapture returns float instead of int
        width = int(vid.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(vid.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = int(vid.get(cv2.CAP_PROP_FPS))
        codec = cv2.VideoWriter_fourcc(*FLAGS.output_format)
        out = cv2.VideoWriter(FLAGS.output, codec, fps, (width, height))

    frame_id = 0
    while True:
        return_value, frame = vid.read()
        if return_value:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(frame)
        else:
            if frame_id == vid.get(cv2.CAP_PROP_FRAME_COUNT):
                print("Video processing complete")
                break
            raise ValueError("No image! Try with another video format")

        frame_size = frame.shape[:2]
        image_data = cv2.resize(frame, (input_size, input_size))
        image_data = image_data / 255.
        image_data = image_data[np.newaxis, ...].astype(np.float32)
        prev_time = time.time()

        if FLAGS.framework == 'tflite':
            interpreter.set_tensor(input_details[0]['index'], image_data)
            interpreter.invoke()
            pred = [interpreter.get_tensor(output_details[i]['index']) for i in range(len(output_details))]
            if FLAGS.model == 'yolov3' and FLAGS.tiny == True:
                boxes, pred_conf = filter_boxes(pred[1], pred[0], score_threshold=0.25,
                                                input_shape=tf.constant([input_size, input_size]))
            else:
                boxes, pred_conf = filter_boxes(pred[0], pred[1], score_threshold=0.25,
                                                input_shape=tf.constant([input_size, input_size]))
        else:
            batch_data = tf.constant(image_data)
            boxes, scores, classes, valid_detections = infer(batch_data, model, STRIDES, ANCHORS, NUM_CLASS, XYSCALE)
        pred_bbox = [boxes.numpy(), scores.numpy(), classes.numpy(), valid_detections.numpy()]
        image = utils.draw_bbox(frame, pred_bbox)
        curr_time = time.time()
        exec_time = curr_time - prev_time
        result = np.asarray(image)
        info = "time: %.2f ms" % (1000 * exec_time)
        logging.info(info)

        result = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        if not FLAGS.dis_cv2_window:
            cv2.namedWindow("result", cv2.WINDOW_AUTOSIZE)
            cv2.imshow("result", result)
            if cv2.waitKey(1) & 0xFF == ord('q'): break
        else:
            bbox_messes = utils.describe_bbox(frame, pred_bbox)
            for mess in bbox_messes:
                logging.info(mess)

        if FLAGS.output:
            out.write(result)

        frame_id += 1


if __name__ == '__main__':
    try:
        app.run(main)
    except SystemExit:
        pass